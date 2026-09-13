"""Real-PostgreSQL tests for the SM-1002 Cognitive Memory Create/Get
surface (ADR-0016, Feature Contract v0.7.0 Native Cognitive Memory).

Proves, against a real PostgreSQL database and the real
``CognitiveMemoryService``/HTTP routes (only the embedding provider is
faked): embedding-before-transaction ordering, synchronous create with no
PipelineRun/graph_outbox row, Get hydration, and the full
``Idempotency-Key`` contract -- optional header, same-key/same-request
replay without a second embedding call, same-key/different-request
conflict, no-key non-deduplication, provider-failure atomicity, and a
barrier-synchronized concurrent-race proof of the authoritative
``UNIQUE`` claim. Requires migrations already applied through 0018.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from sofias_memory.api.errors import DependencyUnavailableError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings, load_settings
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import (
    CognitiveMemoryIdempotency,
    GraphOutbox,
    MemoryItem,
    MemoryProvenance,
    PipelineRun,
    PipelineStep,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.schemas.memories import MemoryCreateRequest
from sofias_memory.services.cognitive_memory import CognitiveMemoryService
from tests.unit._app_factory import create_app

POSTGRES_COGNITIVE_MEMORY_HTTP_ENV = "SOFIAS_MEMORY_RUN_COGNITIVE_MEMORY_HTTP_POSTGRES_TESTS"

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEO4J_PASSWORD_FALLBACK = "8P7nanOVz6vfmrg"
LLM_API_KEY = "sk-fake-test-key"
COGNITIVE_IDEMPOTENCY_HMAC_KEY = "test-cognitive-idempotency-hmac-key-0123456789abcdef"
EMBEDDING_DIMENSIONS = 3072
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}


class FakeEmbeddingClient:
    def __init__(self, *, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self._dimensions = dimensions
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.125] * self._dimensions for _ in texts]


class FailingEmbeddingClient:
    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        raise ConnectionError("simulated embedding provider failure")


class BlockingEmbeddingClient:
    """Blocks inside ``embed_texts`` until the test releases it -- used to
    prove no PostgreSQL session/transaction is opened across the call."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.started.set()
        await self.release.wait()
        return [[0.125] * EMBEDDING_DIMENSIONS for _ in texts]


class BarrierEmbeddingClient:
    """Two concurrent callers both complete embedding before either reaches
    the authoritative transaction -- used to prove the race is resolved by
    the ``UNIQUE(idempotency_key)`` claim, not by luck (no ``sleep``)."""

    def __init__(self, *, participants: int) -> None:
        self._barrier = asyncio.Barrier(participants)

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        await self._barrier.wait()
        return [[0.125] * EMBEDDING_DIMENSIONS for _ in texts]


def _cognitive_memory_http_test_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_COGNITIVE_MEMORY_HTTP_ENV) != "1":
        pytest.skip(f"set {POSTGRES_COGNITIVE_MEMORY_HTTP_ENV}=1 to run this suite")
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return database_url


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    _cognitive_memory_http_test_database_url(dict(os.environ))
    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


async def cleanup(session_factory: AsyncSessionFactory, memory_ids: set[UUID]) -> None:
    if not memory_ids:
        return
    async with session_factory() as session:
        await session.execute(
            delete(CognitiveMemoryIdempotency).where(
                CognitiveMemoryIdempotency.target_memory_id.in_(memory_ids)
                | CognitiveMemoryIdempotency.result_memory_id.in_(memory_ids)
            )
        )
        await session.execute(delete(MemoryItem).where(MemoryItem.superseded_by.in_(memory_ids)))
        await session.execute(delete(MemoryItem).where(MemoryItem.id.in_(memory_ids)))
        await session.commit()


def unique_key(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def build_create_request(**overrides: object) -> MemoryCreateRequest:
    base: dict[str, object] = {
        "memory_type": "profile",
        "scope": "global",
        "content": f"Prefers teal interfaces {uuid4().hex}.",
        "provenance": {
            "origin_kind": "user_asserted",
            "source_system": "sofias-assistant",
            "turn_uuid": str(uuid4()),
        },
    }
    base.update(overrides)
    return MemoryCreateRequest(**base)  # type: ignore[arg-type]


def cognitive_memory_service(
    session_factory: AsyncSessionFactory, embedding_client: object
) -> CognitiveMemoryService:
    return CognitiveMemoryService(
        load_settings(),
        embedding_client=embedding_client,  # type: ignore[arg-type]
        session_factory=session_factory,
    )


async def table_count(session_factory: AsyncSessionFactory, model: type) -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


def make_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key=EXPECTED_API_KEY,
        database_url="postgresql+asyncpg://unused:unused@localhost:5432/unused",
        neo4j_password=os.environ.get("NEO4J_PASSWORD", NEO4J_PASSWORD_FALLBACK),
        llm_api_key=LLM_API_KEY,
        cognitive_idempotency_hmac_key=COGNITIVE_IDEMPOTENCY_HMAC_KEY,
        app_env="test",
    )


def build_app(
    session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding_client_factory: Any = lambda settings: FakeEmbeddingClient(),
) -> Any:
    monkeypatch.setattr(
        "sofias_memory.api.routes.memories.OpenAIEmbeddingClient",
        embedding_client_factory,
    )
    return create_app(
        make_settings(),
        enable_postgres_readiness=False,
        enable_neo4j=False,
        postgres_session_factory=session_factory,
    )


def build_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def create_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "memory_type": "profile",
        "scope": "global",
        "content": f"Prefers teal interfaces {uuid4().hex}.",
        "provenance": {
            "origin_kind": "user_asserted",
            "source_system": "sofias-assistant",
            "turn_uuid": str(uuid4()),
        },
    }
    payload.update(overrides)
    return payload


def test_cognitive_memory_http_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        _cognitive_memory_http_test_database_url({})


# --- HTTP-level Create/Get ---------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_memory_returns_201_and_get_returns_200(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    memory_id: UUID | None = None

    try:
        async with build_client(app) as client:
            payload = create_payload()
            response = await client.post("/api/v1/memories", json=payload, headers=HEADERS)
            assert response.status_code == 201
            body = response.json()
            data = body["data"]
            assert "embedding" not in data
            assert data["memory_type"] == "profile"
            assert data["scope"] == "global"
            assert data["content"] == payload["content"]
            assert data["lifecycle"] == "active"
            assert data["provenance"]["origin_kind"] == "user_asserted"
            assert body["meta"]["request_id"]
            memory_id = UUID(cast(str, data["memory_id"]))

            found = await client.get(f"/api/v1/memories/{memory_id}", headers=HEADERS)
            assert found.status_code == 200
            found_data = found.json()["data"]
            assert found_data["memory_id"] == str(memory_id)
            assert found_data["content"] == payload["content"]

            missing = await client.get(f"/api/v1/memories/{uuid4()}", headers=HEADERS)
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "MEMORY_NOT_FOUND"
    finally:
        await cleanup(postgres_session_factory, {memory_id} if memory_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_memory_persists_exactly_one_item_and_one_provenance_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    fake = FakeEmbeddingClient()
    result = await cognitive_memory_service(postgres_session_factory, fake).create(
        build_create_request(), idempotency_key=None
    )

    try:
        async with postgres_session_factory() as session:
            item = await session.get(MemoryItem, result.memory_id)
            assert item is not None
            assert item.embedding is not None
            assert len(item.embedding) == EMBEDDING_DIMENSIONS
            provenance = await session.get(MemoryProvenance, result.memory_id)
            assert provenance is not None
            assert provenance.origin_kind.value == "user_asserted"
    finally:
        await cleanup(postgres_session_factory, {result.memory_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_memory_creates_no_pipeline_run_or_graph_outbox_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    before = {
        "pipeline_runs": await table_count(postgres_session_factory, PipelineRun),
        "pipeline_steps": await table_count(postgres_session_factory, PipelineStep),
        "graph_outbox": await table_count(postgres_session_factory, GraphOutbox),
    }
    result = await cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient()).create(
        build_create_request(), idempotency_key=None
    )

    try:
        after = {
            "pipeline_runs": await table_count(postgres_session_factory, PipelineRun),
            "pipeline_steps": await table_count(postgres_session_factory, PipelineStep),
            "graph_outbox": await table_count(postgres_session_factory, GraphOutbox),
        }
        assert after == before
    finally:
        await cleanup(postgres_session_factory, {result.memory_id})


# --- embedding-before-transaction ordering -----------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_call_holds_no_session_before_transaction(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session_factory_calls = 0
    real_factory = postgres_session_factory

    def counting_session_factory() -> Any:
        nonlocal session_factory_calls
        session_factory_calls += 1
        return real_factory()

    blocking = BlockingEmbeddingClient()
    service = CognitiveMemoryService(
        load_settings(),
        embedding_client=blocking,
        session_factory=counting_session_factory,  # type: ignore[arg-type]
    )

    task = asyncio.create_task(service.create(build_create_request(), idempotency_key=None))
    memory_id: UUID | None = None
    try:
        await asyncio.wait_for(blocking.started.wait(), timeout=5)
        assert session_factory_calls == 0

        blocking.release.set()
        result = await asyncio.wait_for(task, timeout=5)
        memory_id = result.memory_id
        assert session_factory_calls >= 1
    finally:
        blocking.release.set()
        await cleanup(postgres_session_factory, {memory_id} if memory_id else set())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_failure_leaves_zero_authoritative_rows(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    before = await table_count(postgres_session_factory, MemoryItem)

    with pytest.raises(DependencyUnavailableError):
        await cognitive_memory_service(postgres_session_factory, FailingEmbeddingClient()).create(
            build_create_request(), idempotency_key=None
        )

    after = await table_count(postgres_session_factory, MemoryItem)
    assert after == before


# --- Idempotency-Key contract -------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_absent_idempotency_key_never_deduplicates(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    request = build_create_request()
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())

    first = await service.create(request, idempotency_key=None)
    second = await service.create(request, idempotency_key=None)

    try:
        assert first.memory_id != second.memory_id
    finally:
        await cleanup(postgres_session_factory, {first.memory_id, second.memory_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_key_same_request_replays_without_re_embedding(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    fake = FakeEmbeddingClient()
    service = cognitive_memory_service(postgres_session_factory, fake)
    request = build_create_request()
    key = unique_key("create")

    first = await service.create(request, idempotency_key=key)
    calls_after_first = len(fake.calls)
    second = await service.create(request, idempotency_key=key)

    try:
        assert second.memory_id == first.memory_id
        assert len(fake.calls) == calls_after_first  # no second embedding call
        assert await table_count(postgres_session_factory, CognitiveMemoryIdempotency) >= 1
    finally:
        await cleanup(postgres_session_factory, {first.memory_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_key_different_request_returns_409_without_second_item(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    from sofias_memory.api.errors import SofiasMemoryError

    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    key = unique_key("create")
    first = await service.create(build_create_request(), idempotency_key=key)
    count_after_first = await table_count(postgres_session_factory, MemoryItem)

    try:
        with pytest.raises(SofiasMemoryError) as exc_info:
            await service.create(
                build_create_request(content="A genuinely different fact."),
                idempotency_key=key,
            )
        assert exc_info.value.code.value == "IDEMPOTENCY_CONFLICT"
        assert await table_count(postgres_session_factory, MemoryItem) == count_after_first
    finally:
        await cleanup(postgres_session_factory, {first.memory_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reserved_sys_key_is_rejected(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    from sofias_memory.api.errors import SofiasMemoryError

    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    with pytest.raises(SofiasMemoryError) as exc_info:
        await service.create(build_create_request(), idempotency_key="sys:internal")
    assert exc_info.value.code.value == "RESERVED_IDEMPOTENCY_KEY_NAMESPACE"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_same_key_same_request_converges_to_one_winner(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Two concurrent Create calls, same Idempotency-Key and same normalized
    request, both pass an empty pre-check and both complete embedding before
    either reaches the authoritative transaction (barrier-synchronized, no
    ``sleep``). Exactly one MemoryItem/provenance/ledger row must exist and
    both callers must converge on the same ``memory_id``."""

    request = build_create_request()
    key = unique_key("race")
    barrier_client = BarrierEmbeddingClient(participants=2)
    service_a = CognitiveMemoryService(
        load_settings(), embedding_client=barrier_client, session_factory=postgres_session_factory
    )
    service_b = CognitiveMemoryService(
        load_settings(), embedding_client=barrier_client, session_factory=postgres_session_factory
    )

    results = await asyncio.gather(
        service_a.create(request, idempotency_key=key),
        service_b.create(request, idempotency_key=key),
    )

    memory_id = results[0].memory_id
    try:
        assert results[1].memory_id == memory_id
        async with postgres_session_factory() as session:
            provenance_count = await session.scalar(
                select(func.count())
                .select_from(MemoryProvenance)
                .where(MemoryProvenance.memory_id == memory_id)
            )
            ledger_count = await session.scalar(
                select(func.count())
                .select_from(CognitiveMemoryIdempotency)
                .where(CognitiveMemoryIdempotency.idempotency_key == key)
            )
        assert provenance_count == 1
        assert ledger_count == 1
    finally:
        await cleanup(postgres_session_factory, {memory_id})
