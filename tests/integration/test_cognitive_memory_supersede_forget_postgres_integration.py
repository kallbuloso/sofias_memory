"""Real-PostgreSQL tests for the SM-1004 atomic Supersession and destructive
precise Forget surface (ADR-0016, Feature Contract v0.7.0 Native Cognitive
Memory SS 15/16/17).

Proves, against a real PostgreSQL database and the real
``CognitiveMemoryService`` (only the embedding provider is faked):
Supersede's replacement inheritance/atomicity, the same-operation-replay-
precedes-lifecycle-conflict invariant under both sequential retry and real
concurrent racing, Forget's destructive scrub of MemoryItem/MemoryProvenance
in one transaction, FORGOTTEN's resource-state idempotent no-op (including
under a brand-new key, and the same-key-conflict precedence over that
no-op), lineage independence between Forget and Supersede, and typed-recall
exclusion after a REAL Forget (not a manually-inserted tombstone fixture).
Requires migrations already applied through 0018.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
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
from sofias_memory.schemas.memories import (
    MemoryCreateRequest,
    MemoryRecallRequest,
    MemorySupersedeRequest,
)
from sofias_memory.services.cognitive_memory import CognitiveMemoryService
from sofias_memory.services.cognitive_memory_recall import CognitiveMemoryRecallService
from tests.unit._app_factory import create_app

POSTGRES_ENV = "SOFIAS_MEMORY_RUN_COGNITIVE_MEMORY_SUPERSEDE_FORGET_POSTGRES_TESTS"

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
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.started.set()
        await self.release.wait()
        return [[0.125] * EMBEDDING_DIMENSIONS for _ in texts]


class BarrierEmbeddingClient:
    """Every concurrent caller completes embedding before any of them reach
    the authoritative transaction -- proves races are resolved by the
    ``UNIQUE(idempotency_key)``/row-lock claim, never by luck (no
    ``sleep``)."""

    def __init__(self, *, participants: int) -> None:
        self._barrier = asyncio.Barrier(participants)

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        await self._barrier.wait()
        return [[0.125] * EMBEDDING_DIMENSIONS for _ in texts]


def _test_database_url(env: dict[str, str]) -> str:
    if env.get(POSTGRES_ENV) != "1":
        pytest.skip(f"set {POSTGRES_ENV}=1 to run this suite")
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set DATABASE_URL to a dedicated discardable PostgreSQL database")
    return database_url


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    _test_database_url(dict(os.environ))
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
        "content": f"Original content {uuid4().hex}.",
        "provenance": {
            "origin_kind": "user_asserted",
            "source_system": "sofias-assistant",
            "turn_uuid": str(uuid4()),
        },
    }
    base.update(overrides)
    return MemoryCreateRequest(**base)  # type: ignore[arg-type]


def build_supersede_request(**overrides: object) -> MemorySupersedeRequest:
    base: dict[str, object] = {
        "content": f"Replacement content {uuid4().hex}.",
        "provenance": {
            "origin_kind": "user_asserted",
            "source_system": "sofias-assistant",
            "turn_uuid": str(uuid4()),
        },
    }
    base.update(overrides)
    return MemorySupersedeRequest(**base)  # type: ignore[arg-type]


def cognitive_memory_service(
    session_factory: AsyncSessionFactory, embedding_client: object
) -> CognitiveMemoryService:
    return CognitiveMemoryService(
        load_settings(),
        embedding_client=embedding_client,  # type: ignore[arg-type]
        session_factory=session_factory,
    )


def recall_service(
    session_factory: AsyncSessionFactory, embedding_client: object
) -> CognitiveMemoryRecallService:
    return CognitiveMemoryRecallService(
        load_settings(),
        embedding_client=embedding_client,  # type: ignore[arg-type]
        session_factory=session_factory,
    )


async def create_old(
    session_factory: AsyncSessionFactory, embedding_client: object = None, **overrides: object
) -> UUID:
    client = embedding_client or FakeEmbeddingClient()
    result = await cognitive_memory_service(session_factory, client).create(
        build_create_request(**overrides), idempotency_key=None
    )
    return result.memory_id


async def fetch_row(session_factory: AsyncSessionFactory, memory_id: UUID) -> MemoryItem | None:
    async with session_factory() as session:
        return await session.get(MemoryItem, memory_id)


async def fetch_provenance(
    session_factory: AsyncSessionFactory, memory_id: UUID
) -> MemoryProvenance | None:
    async with session_factory() as session:
        return await session.get(MemoryProvenance, memory_id)


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


def test_supersede_forget_postgres_tests_skip_without_opt_in() -> None:
    with pytest.raises(pytest.skip.Exception):
        _test_database_url({})


# --- Supersede: happy path / inheritance / atomicity --------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_happy_path_inherits_type_scope_and_preserves_old(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(
        postgres_session_factory, memory_type="semantic", scope="project:alpha"
    )
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    old_before = await fetch_row(postgres_session_factory, old_id)
    assert old_before is not None
    original_content = old_before.content

    replacement_id: UUID | None = None
    try:
        result = await service.supersede(
            old_id, build_supersede_request(content="New fact."), idempotency_key=None
        )
        assert result.old.memory_id == old_id
        assert result.old.lifecycle.value == "superseded"
        assert result.replacement.lifecycle.value == "active"
        assert result.replacement.memory_type.value == "semantic"
        assert result.replacement.scope == "project:alpha"
        assert result.replacement.content == "New fact."
        replacement_id = result.replacement.memory_id
        assert replacement_id is not None

        old_after = await fetch_row(postgres_session_factory, old_id)
        assert old_after is not None
        assert old_after.lifecycle.value == "superseded"
        assert old_after.superseded_by == replacement_id
        assert old_after.superseded_at is not None
        # Old content/provenance are untouched by Supersede -- only Forget destroys them.
        assert old_after.content == original_content
        assert old_after.embedding is not None

        replacement_provenance = await fetch_provenance(postgres_session_factory, replacement_id)
        old_provenance = await fetch_provenance(postgres_session_factory, old_id)
        assert replacement_provenance is not None
        assert old_provenance is not None
        assert replacement_provenance.memory_id != old_provenance.memory_id
    finally:
        await cleanup(
            postgres_session_factory, {old_id} | ({replacement_id} if replacement_id else set())
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_replacement_can_be_superseded_again_forming_linear_chain(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(postgres_session_factory)
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    memory_ids = {old_id}
    try:
        first = await service.supersede(
            old_id, build_supersede_request(content="v2"), idempotency_key=None
        )
        memory_ids.add(first.replacement.memory_id)
        second = await service.supersede(
            first.replacement.memory_id, build_supersede_request(content="v3"), idempotency_key=None
        )
        memory_ids.add(second.replacement.memory_id)

        assert second.old.memory_id == first.replacement.memory_id
        assert second.old.lifecycle.value == "superseded"
        assert second.replacement.content == "v3"

        original_old = await fetch_row(postgres_session_factory, old_id)
        assert original_old is not None
        assert original_old.superseded_by == first.replacement.memory_id
    finally:
        await cleanup(postgres_session_factory, memory_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_target_superseded_returns_409_state_conflict(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(postgres_session_factory)
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    memory_ids = {old_id}
    try:
        first = await service.supersede(old_id, build_supersede_request(), idempotency_key=None)
        memory_ids.add(first.replacement.memory_id)

        with pytest.raises(SofiasMemoryError) as exc_info:
            await service.supersede(old_id, build_supersede_request(), idempotency_key=None)
        assert exc_info.value.code.value == "MEMORY_STATE_CONFLICT"
    finally:
        await cleanup(postgres_session_factory, memory_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_target_forgotten_returns_409_state_conflict(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(postgres_session_factory)
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    try:
        await service.forget(old_id, idempotency_key=None)
        with pytest.raises(SofiasMemoryError) as exc_info:
            await service.supersede(old_id, build_supersede_request(), idempotency_key=None)
        assert exc_info.value.code.value == "MEMORY_STATE_CONFLICT"
    finally:
        await cleanup(postgres_session_factory, {old_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_missing_returns_404(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    with pytest.raises(SofiasMemoryError) as exc_info:
        await service.supersede(uuid4(), build_supersede_request(), idempotency_key=None)
    assert exc_info.value.code.value == "MEMORY_NOT_FOUND"


# --- Supersede: idempotency, including same-operation replay precedence ------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_same_key_same_request_replay_after_already_superseded(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """The mandatory acceptance scenario (Feature Contract SS 15.2): a
    same-key/same-request retry AFTER the winner already moved ``old`` to
    SUPERSEDED must replay the original outcome -- never a false
    MEMORY_STATE_CONFLICT."""

    old_id = await create_old(postgres_session_factory)
    fake = FakeEmbeddingClient()
    service = cognitive_memory_service(postgres_session_factory, fake)
    request = build_supersede_request(content="the one true replacement")
    key = unique_key("supersede")
    memory_ids = {old_id}
    try:
        first = await service.supersede(old_id, request, idempotency_key=key)
        memory_ids.add(first.replacement.memory_id)
        calls_after_first = len(fake.calls)

        second = await service.supersede(old_id, request, idempotency_key=key)

        assert second.replacement.memory_id == first.replacement.memory_id
        assert second.old.memory_id == old_id
        assert second.old.lifecycle.value == "superseded"
        assert len(fake.calls) == calls_after_first  # no second embedding call
    finally:
        await cleanup(postgres_session_factory, memory_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_same_key_different_request_returns_409_idempotency_conflict(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(postgres_session_factory)
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    key = unique_key("supersede")
    memory_ids = {old_id}
    try:
        first = await service.supersede(
            old_id, build_supersede_request(content="v2"), idempotency_key=key
        )
        memory_ids.add(first.replacement.memory_id)

        with pytest.raises(SofiasMemoryError) as exc_info:
            await service.supersede(
                old_id,
                build_supersede_request(content="a genuinely different edit"),
                idempotency_key=key,
            )
        assert exc_info.value.code.value == "IDEMPOTENCY_CONFLICT"
    finally:
        await cleanup(postgres_session_factory, memory_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_same_key_reused_for_different_target_returns_409(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id_a = await create_old(postgres_session_factory)
    old_id_b = await create_old(postgres_session_factory)
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    key = unique_key("supersede")
    request = build_supersede_request(content="same payload")
    memory_ids = {old_id_a, old_id_b}
    try:
        first = await service.supersede(old_id_a, request, idempotency_key=key)
        memory_ids.add(first.replacement.memory_id)

        with pytest.raises(SofiasMemoryError) as exc_info:
            await service.supersede(old_id_b, request, idempotency_key=key)
        assert exc_info.value.code.value == "IDEMPOTENCY_CONFLICT"
        # old_id_b must remain untouched.
        old_b_after = await fetch_row(postgres_session_factory, old_id_b)
        assert old_b_after is not None
        assert old_b_after.lifecycle.value == "active"
    finally:
        await cleanup(postgres_session_factory, memory_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_same_key_reused_from_create_returns_409(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    key = unique_key("cross-op")
    memory_ids: set[UUID] = set()
    try:
        created = await service.create(build_create_request(), idempotency_key=key)
        memory_ids.add(created.memory_id)

        with pytest.raises(SofiasMemoryError) as exc_info:
            await service.supersede(
                created.memory_id, build_supersede_request(), idempotency_key=key
            )
        assert exc_info.value.code.value == "IDEMPOTENCY_CONFLICT"
    finally:
        await cleanup(postgres_session_factory, memory_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_reserved_sys_key_rejected(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(postgres_session_factory)
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    try:
        with pytest.raises(SofiasMemoryError) as exc_info:
            await service.supersede(
                old_id, build_supersede_request(), idempotency_key="sys:internal"
            )
        assert exc_info.value.code.value == "RESERVED_IDEMPOTENCY_KEY_NAMESPACE"
    finally:
        await cleanup(postgres_session_factory, {old_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_missing_target_with_key_leaves_no_ledger_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    key = unique_key("missing-target")
    with pytest.raises(SofiasMemoryError):
        await service.supersede(uuid4(), build_supersede_request(), idempotency_key=key)

    async with postgres_session_factory() as session:
        ledger = await session.scalar(
            select(CognitiveMemoryIdempotency).where(
                CognitiveMemoryIdempotency.idempotency_key == key
            )
        )
    assert ledger is None


# --- Supersede: embedding ordering / provider failure --------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_provider_call_holds_no_session_before_transaction(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(postgres_session_factory)
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

    task = asyncio.create_task(
        service.supersede(old_id, build_supersede_request(), idempotency_key=None)
    )
    replacement_id: UUID | None = None
    try:
        await asyncio.wait_for(blocking.started.wait(), timeout=5)
        assert session_factory_calls == 0

        blocking.release.set()
        result = await asyncio.wait_for(task, timeout=5)
        replacement_id = result.replacement.memory_id
        assert session_factory_calls >= 1
    finally:
        blocking.release.set()
        await cleanup(
            postgres_session_factory, {old_id} | ({replacement_id} if replacement_id else set())
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_provider_failure_leaves_old_active_no_replacement(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(postgres_session_factory)
    service = cognitive_memory_service(postgres_session_factory, FailingEmbeddingClient())
    try:
        with pytest.raises(DependencyUnavailableError):
            await service.supersede(old_id, build_supersede_request(), idempotency_key=None)

        old_after = await fetch_row(postgres_session_factory, old_id)
        assert old_after is not None
        assert old_after.lifecycle.value == "active"
        assert old_after.superseded_by is None
    finally:
        await cleanup(postgres_session_factory, {old_id})


# --- Supersede: basic deterministic concurrency ---------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_concurrent_same_key_same_request_converges_to_one_winner(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(postgres_session_factory)
    request = build_supersede_request(content="the winning content")
    key = unique_key("race-same")
    barrier_client = BarrierEmbeddingClient(participants=2)
    service_a = CognitiveMemoryService(
        load_settings(), embedding_client=barrier_client, session_factory=postgres_session_factory
    )
    service_b = CognitiveMemoryService(
        load_settings(), embedding_client=barrier_client, session_factory=postgres_session_factory
    )

    results = await asyncio.gather(
        service_a.supersede(old_id, request, idempotency_key=key),
        service_b.supersede(old_id, request, idempotency_key=key),
    )
    replacement_id = results[0].replacement.memory_id
    try:
        assert results[1].replacement.memory_id == replacement_id
        async with postgres_session_factory() as session:
            old_row = await session.get(MemoryItem, old_id)
            assert old_row is not None
            assert old_row.superseded_by == replacement_id
            ledger_count = await session.scalar(
                select(CognitiveMemoryIdempotency.id).where(
                    CognitiveMemoryIdempotency.idempotency_key == key
                )
            )
            assert ledger_count is not None
    finally:
        await cleanup(postgres_session_factory, {old_id, replacement_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_concurrent_distinct_operations_one_wins_one_conflicts(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(postgres_session_factory)
    barrier_client = BarrierEmbeddingClient(participants=2)
    service_a = CognitiveMemoryService(
        load_settings(), embedding_client=barrier_client, session_factory=postgres_session_factory
    )
    service_b = CognitiveMemoryService(
        load_settings(), embedding_client=barrier_client, session_factory=postgres_session_factory
    )

    results = await asyncio.gather(
        service_a.supersede(
            old_id, build_supersede_request(content="A wins or loses"), idempotency_key=None
        ),
        service_b.supersede(
            old_id, build_supersede_request(content="B wins or loses"), idempotency_key=None
        ),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], SofiasMemoryError)
    assert failures[0].code.value == "MEMORY_STATE_CONFLICT"

    winner = successes[0]
    replacement_id = winner.replacement.memory_id  # type: ignore[union-attr]
    try:
        old_row = await fetch_row(postgres_session_factory, old_id)
        assert old_row is not None
        assert old_row.superseded_by == replacement_id
    finally:
        await cleanup(postgres_session_factory, {old_id, replacement_id})


# --- Forget: lifecycle transitions -----------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_active_item_transitions_and_creates_no_replacement(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(postgres_session_factory)
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    try:
        result = await service.forget(old_id, idempotency_key=None)
        assert result.memory_id == old_id
        assert result.lifecycle.value == "forgotten"
        assert result.superseded_at is None
        assert result.superseded_by is None

        row = await fetch_row(postgres_session_factory, old_id)
        assert row is not None
        assert row.superseded_at is None
        assert row.superseded_by is None
    finally:
        await cleanup(postgres_session_factory, {old_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_of_superseded_item_preserves_lineage_and_leaves_replacement_untouched(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(postgres_session_factory)
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    memory_ids = {old_id}
    try:
        superseded = await service.supersede(
            old_id,
            build_supersede_request(content="replacement stays intact"),
            idempotency_key=None,
        )
        replacement_id = superseded.replacement.memory_id
        memory_ids.add(replacement_id)

        result = await service.forget(old_id, idempotency_key=None)
        assert result.lifecycle.value == "forgotten"
        assert result.superseded_at is not None
        assert result.superseded_by == replacement_id

        replacement_row = await fetch_row(postgres_session_factory, replacement_id)
        assert replacement_row is not None
        assert replacement_row.lifecycle.value == "active"
        assert replacement_row.content == "replacement stays intact"
        assert replacement_row.embedding is not None
    finally:
        await cleanup(postgres_session_factory, memory_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_missing_returns_404(postgres_session_factory: AsyncSessionFactory) -> None:
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    with pytest.raises(SofiasMemoryError) as exc_info:
        await service.forget(uuid4(), idempotency_key=None)
    assert exc_info.value.code.value == "MEMORY_NOT_FOUND"


# --- Forget: FORGOTTEN resource-state idempotency -------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_forgotten_with_same_key_replays(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(postgres_session_factory)
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    key = unique_key("forget")
    try:
        first = await service.forget(old_id, idempotency_key=key)
        second = await service.forget(old_id, idempotency_key=key)
        assert second.memory_id == first.memory_id
        assert second.forgotten_at == first.forgotten_at
    finally:
        await cleanup(postgres_session_factory, {old_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_forgotten_with_new_key_is_200_no_op_no_second_mutation(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(postgres_session_factory)
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    try:
        first = await service.forget(old_id, idempotency_key=unique_key("forget-a"))
        second = await service.forget(old_id, idempotency_key=unique_key("forget-b"))

        assert second.memory_id == old_id
        assert second.lifecycle.value == "forgotten"
        assert second.forgotten_at == first.forgotten_at  # not re-stamped
        assert second.created_at == first.created_at
    finally:
        await cleanup(postgres_session_factory, {old_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_reused_key_different_request_returns_409_before_state_noop(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Same-key conflict semantics have precedence over the FORGOTTEN
    resource-state no-op (Feature Contract SS 16/19): reusing a key already
    bound to a DIFFERENT target must never silently succeed as a no-op,
    even if the second target happens to already be FORGOTTEN."""

    old_id_a = await create_old(postgres_session_factory)
    old_id_b = await create_old(postgres_session_factory)
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    key = unique_key("forget-reused")
    try:
        await service.forget(old_id_a, idempotency_key=key)
        await service.forget(old_id_b, idempotency_key=None)  # b is already FORGOTTEN

        with pytest.raises(SofiasMemoryError) as exc_info:
            await service.forget(old_id_b, idempotency_key=key)
        assert exc_info.value.code.value == "IDEMPOTENCY_CONFLICT"
    finally:
        await cleanup(postgres_session_factory, {old_id_a, old_id_b})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_reserved_sys_key_rejected(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    old_id = await create_old(postgres_session_factory)
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    try:
        with pytest.raises(SofiasMemoryError) as exc_info:
            await service.forget(old_id, idempotency_key="sys:internal")
        assert exc_info.value.code.value == "RESERVED_IDEMPOTENCY_KEY_NAMESPACE"
    finally:
        await cleanup(postgres_session_factory, {old_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_concurrent_two_requests_different_fresh_keys_converge(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Forget never calls the embedding provider, so there is no external
    call to block on for barrier synchronization -- both calls are simply
    submitted together via ``asyncio.gather``, and PostgreSQL's row lock
    (``get_by_id_for_update``) is solely responsible for serializing them
    correctly regardless of exact interleaving (no ``sleep``)."""

    old_id = await create_old(postgres_session_factory)
    service_a = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    service_b = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())

    results = await asyncio.gather(
        service_a.forget(old_id, idempotency_key=unique_key("concurrent-forget-a")),
        service_b.forget(old_id, idempotency_key=unique_key("concurrent-forget-b")),
    )
    try:
        assert results[0].memory_id == old_id
        assert results[1].memory_id == old_id
        assert results[0].lifecycle.value == "forgotten"
        assert results[1].lifecycle.value == "forgotten"
        assert results[0].forgotten_at == results[1].forgotten_at  # single destructive mutation

        row = await fetch_row(postgres_session_factory, old_id)
        assert row is not None
        assert row.lifecycle.value == "forgotten"
        assert row.content is None
    finally:
        await cleanup(postgres_session_factory, {old_id})


# --- Forget: destructive scrub / privacy proof ----------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_scrubs_all_cognitive_and_provenance_columns(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    sentinel_conversation = uuid4()
    sentinel_turn = uuid4()
    sentinel_task = uuid4()
    sentinel_confirmation = f"confirmation-sentinel-{uuid4().hex}"
    sentinel_source_ref = f"source-ref-sentinel-{uuid4().hex}"
    sentinel_content = f"SENTINEL CONTENT {uuid4().hex}"
    sentinel_scope = "project:sentinel-scope-" + uuid4().hex[:12]
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)

    old_id = await create_old(
        postgres_session_factory,
        content=sentinel_content,
        scope=sentinel_scope,
        confidence=0.42,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2026, 6, 1, tzinfo=UTC),
        provenance={
            "origin_kind": "tool_observed",
            "source_system": "sofias-assistant",
            "conversation_uuid": str(sentinel_conversation),
            "turn_uuid": str(sentinel_turn),
            "task_uuid": str(sentinel_task),
            "confirmation_ref": sentinel_confirmation,
            "source_ref": sentinel_source_ref,
            "observed_at": observed_at.isoformat(),
        },
    )
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    try:
        await service.forget(old_id, idempotency_key=None)

        row = await fetch_row(postgres_session_factory, old_id)
        assert row is not None
        assert row.content is None
        assert row.embedding is None
        assert row.scope is None
        assert row.confidence is None
        assert row.valid_from is None
        assert row.valid_until is None
        assert row.lifecycle.value == "forgotten"
        assert row.forgotten_at is not None
        assert row.memory_type is not None
        assert row.created_at is not None

        provenance = await fetch_provenance(postgres_session_factory, old_id)
        assert provenance is not None
        assert provenance.conversation_uuid is None
        assert provenance.turn_uuid is None
        assert provenance.task_uuid is None
        assert provenance.confirmation_ref is None
        assert provenance.source_ref is None
        assert provenance.observed_at is None
        assert provenance.origin_kind.value == "tool_observed"
        assert provenance.source_system == "sofias-assistant"

        # Direct structural proof that no sentinel leaked anywhere reachable.
        async with postgres_session_factory() as session:
            memory_row = await session.get(MemoryItem, old_id)
            provenance_row = await session.get(MemoryProvenance, old_id)
            assert memory_row is not None and provenance_row is not None
            memory_repr = repr(memory_row.__dict__)
            provenance_repr = repr(provenance_row.__dict__)
            for sentinel in (
                sentinel_content,
                sentinel_scope,
                sentinel_confirmation,
                sentinel_source_ref,
                str(sentinel_conversation),
                str(sentinel_turn),
                str(sentinel_task),
            ):
                assert sentinel not in memory_repr
                assert sentinel not in provenance_repr

            ledger_rows = (
                await session.scalars(
                    select(CognitiveMemoryIdempotency).where(
                        CognitiveMemoryIdempotency.target_memory_id == old_id
                    )
                )
            ).all()
            for ledger_row in ledger_rows:
                ledger_repr = repr(
                    {
                        "idempotency_key": ledger_row.idempotency_key,
                        "operation": ledger_row.operation,
                        "request_digest": ledger_row.request_digest,
                    }
                )
                for sentinel in (
                    sentinel_content,
                    sentinel_scope,
                    sentinel_confirmation,
                    sentinel_source_ref,
                ):
                    assert sentinel not in ledger_repr
                # The digest is a fixed-length hex string, never raw content.
                assert len(ledger_row.request_digest) == 64
                assert all(c in "0123456789abcdef" for c in ledger_row.request_digest)
    finally:
        await cleanup(postgres_session_factory, {old_id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_forget_ledger_privacy_with_idempotency_key(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    sentinel_content = f"LEDGER SENTINEL {uuid4().hex}"
    old_id = await create_old(postgres_session_factory, content=sentinel_content)
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    key = unique_key("forget-privacy")
    try:
        await service.forget(old_id, idempotency_key=key)
        async with postgres_session_factory() as session:
            ledger = await session.scalar(
                select(CognitiveMemoryIdempotency).where(
                    CognitiveMemoryIdempotency.idempotency_key == key
                )
            )
            assert ledger is not None
            assert ledger.operation.value == "forget"
            assert ledger.target_memory_id == old_id
            assert sentinel_content not in ledger.request_digest
            assert len(ledger.request_digest) == 64
    finally:
        await cleanup(postgres_session_factory, {old_id})


# --- Recall exclusion after a REAL Forget ---------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recall_excludes_item_after_real_forget(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    query_text = "typed recall after real forget"
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[0] = 1.0

    class DeterministicEmbeddingClient:
        async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
            return [list(vector) for _ in texts]

    scope = "project:forget-recall-" + uuid4().hex[:12]
    client = DeterministicEmbeddingClient()
    service = cognitive_memory_service(postgres_session_factory, client)
    created = await service.create(
        build_create_request(scope=scope, content="will be forgotten"), idempotency_key=None
    )
    old_id = created.memory_id
    try:
        before_forget = datetime.now(UTC)
        forgotten = await service.forget(old_id, idempotency_key=None)
        assert forgotten.forgotten_at is not None

        recall = recall_service(postgres_session_factory, client)
        for include_superseded in (False, True):
            current = await recall.recall(
                _recall_request(
                    query=query_text, scopes=[scope], include_superseded=include_superseded
                )
            )
            assert old_id not in {item.memory.memory_id for item in current.items}

            historical = await recall.recall(
                _recall_request(
                    query=query_text,
                    scopes=[scope],
                    as_of=before_forget,
                    include_superseded=include_superseded,
                )
            )
            assert old_id not in {item.memory.memory_id for item in historical.items}
    finally:
        await cleanup(postgres_session_factory, {old_id})


def _recall_request(**overrides: object) -> MemoryRecallRequest:
    base: dict[str, object] = {"query": "placeholder", "scopes": ["global"]}
    base.update(overrides)
    return MemoryRecallRequest(**base)  # type: ignore[arg-type]


# --- HTTP-level smoke: Create -> Supersede -> GET -> GET ------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_http_create_supersede_get_old_get_replacement(
    postgres_session_factory: AsyncSessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = build_app(postgres_session_factory, monkeypatch)
    old_id: UUID | None = None
    replacement_id: UUID | None = None
    try:
        async with build_client(app) as client:
            create_response = await client.post(
                "/api/v1/memories",
                headers=HEADERS,
                json={
                    "memory_type": "profile",
                    "scope": "global",
                    "content": "HTTP smoke original.",
                    "provenance": {
                        "origin_kind": "user_asserted",
                        "source_system": "sofias-assistant",
                        "turn_uuid": str(uuid4()),
                    },
                },
            )
            assert create_response.status_code == 201
            old_id = UUID(create_response.json()["data"]["memory_id"])

            supersede_response = await client.post(
                f"/api/v1/memories/{old_id}/supersede",
                headers=HEADERS,
                json={
                    "content": "HTTP smoke replacement.",
                    "provenance": {
                        "origin_kind": "user_asserted",
                        "source_system": "sofias-assistant",
                        "turn_uuid": str(uuid4()),
                    },
                },
            )
            assert supersede_response.status_code == 200
            supersede_data = supersede_response.json()["data"]
            assert supersede_data["old"]["lifecycle"] == "superseded"
            assert supersede_data["replacement"]["lifecycle"] == "active"
            replacement_id = UUID(supersede_data["replacement"]["memory_id"])

            old_get = await client.get(f"/api/v1/memories/{old_id}", headers=HEADERS)
            assert old_get.status_code == 200
            assert old_get.json()["data"]["lifecycle"] == "superseded"
            assert old_get.json()["data"]["superseded_by"] == str(replacement_id)

            replacement_get = await client.get(
                f"/api/v1/memories/{replacement_id}", headers=HEADERS
            )
            assert replacement_get.status_code == 200
            assert replacement_get.json()["data"]["lifecycle"] == "active"
            assert replacement_get.json()["data"]["content"] == "HTTP smoke replacement."

            forget_response = await client.post(
                f"/api/v1/memories/{old_id}/forget", headers=HEADERS
            )
            assert forget_response.status_code == 200
            assert forget_response.json()["data"]["lifecycle"] == "forgotten"
            assert forget_response.json()["data"]["content"] is None

            replacement_after_forget = await client.get(
                f"/api/v1/memories/{replacement_id}", headers=HEADERS
            )
            assert replacement_after_forget.status_code == 200
            assert replacement_after_forget.json()["data"]["lifecycle"] == "active"
            assert replacement_after_forget.json()["data"]["content"] == "HTTP smoke replacement."
    finally:
        await cleanup(
            postgres_session_factory,
            ({old_id} if old_id else set()) | ({replacement_id} if replacement_id else set()),
        )


# --- Regression: no PipelineRun/graph_outbox from Supersede/Forget -------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersede_and_forget_create_no_pipeline_run_or_graph_outbox_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    async def counts() -> dict[str, int]:
        async with postgres_session_factory() as session:
            return {
                "pipeline_runs": int(
                    await session.scalar(select(func.count()).select_from(PipelineRun)) or 0
                ),
                "pipeline_steps": int(
                    await session.scalar(select(func.count()).select_from(PipelineStep)) or 0
                ),
                "graph_outbox": int(
                    await session.scalar(select(func.count()).select_from(GraphOutbox)) or 0
                ),
            }

    old_id = await create_old(postgres_session_factory)
    service = cognitive_memory_service(postgres_session_factory, FakeEmbeddingClient())
    before = await counts()
    memory_ids = {old_id}
    try:
        superseded = await service.supersede(
            old_id, build_supersede_request(), idempotency_key=None
        )
        memory_ids.add(superseded.replacement.memory_id)
        await service.forget(superseded.replacement.memory_id, idempotency_key=None)

        after = await counts()
        assert after == before
    finally:
        await cleanup(postgres_session_factory, memory_ids)
