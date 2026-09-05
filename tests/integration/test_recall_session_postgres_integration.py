"""Real-PostgreSQL tests for SM-604 Recall/Session admission and snapshot
transaction boundaries.

Proves what a fake in-memory Unit of Work cannot: that the Session admission
barrier (``SELECT ... FOR UPDATE``) genuinely linearizes lazy Session
creation and archive races against real PostgreSQL row locking, that the
Session Context snapshot taken during admission is truly frozen before any
external I/O runs (a concurrent append after admission never leaks into that
Recall's generation), and that an archive completing after admission never
retroactively cancels an in-flight Recall. Requires migrations already
applied through 0013 -- no new migration is introduced by SM-604.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import load_settings
from sofias_memory.domain import SessionStatus
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Dataset, Query, Session
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.recall import RecallRequest
from sofias_memory.schemas.session_entries import SessionEntryCreateRequest
from sofias_memory.schemas.sessions import SessionCreateRequest, SessionResult
from sofias_memory.services.recall import RecallService
from sofias_memory.services.session_entries import SessionEntryService
from sofias_memory.services.sessions import SessionService

RECALL_SESSION_POSTGRES_TESTS_ENV = "SOFIAS_MEMORY_RUN_RECALL_SESSION_POSTGRES_TESTS"


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    if os.environ.get(RECALL_SESSION_POSTGRES_TESTS_ENV) != "1":
        pytest.skip(f"set {RECALL_SESSION_POSTGRES_TESTS_ENV}=1 to run Recall/Session tests")

    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


class FakeEmbeddingClient:
    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.1] * self.dimensions for _ in texts]


class RecordingRagAnswerClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def answer(self, query: str, context: str) -> str:
        self.calls.append((query, context))
        return "Grounded answer."


class SideEffectRagAnswerClient:
    """Runs an arbitrary async side effect (e.g. a concurrent append or
    archive against the real database) from inside generation, after
    Session admission has already committed -- this is how the tests below
    observe whether the snapshot/admission boundary actually holds."""

    def __init__(self, side_effect: object) -> None:
        self._side_effect = side_effect
        self.calls: list[tuple[str, str]] = []

    async def answer(self, query: str, context: str) -> str:
        self.calls.append((query, context))
        await self._side_effect()  # type: ignore[operator]
        return "Grounded answer."


async def cleanup_sessions(session_factory: AsyncSessionFactory, session_ids: set[UUID]) -> None:
    if not session_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Session).where(Session.id.in_(session_ids)))
        await session.commit()


async def cleanup_queries(session_factory: AsyncSessionFactory, query_ids: set[UUID]) -> None:
    if not query_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Query).where(Query.id.in_(query_ids)))
        await session.commit()


async def cleanup_datasets(session_factory: AsyncSessionFactory, dataset_ids: set[UUID]) -> None:
    if not dataset_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Dataset).where(Dataset.id.in_(dataset_ids)))
        await session.commit()


async def create_active_session(session_factory: AsyncSessionFactory) -> SessionResult:
    return await SessionService(session_factory=session_factory).create_session(
        SessionCreateRequest(session_id=f"sm604-{uuid4().hex}")
    )


async def seed_dataset(session_factory: AsyncSessionFactory) -> Dataset:
    dataset = Dataset(
        id=uuid4(),
        name=f"sm604-{uuid4().hex}",
        slug=f"sm604-{uuid4().hex}",
        description=None,
        active_generation=0,
    )
    async with PostgresUnitOfWork(session_factory) as uow:
        await uow.datasets.add(dataset)
        await uow.commit()
    return dataset


def recall_service_for(
    session_factory: AsyncSessionFactory,
    *,
    embedding_client: FakeEmbeddingClient,
    rag_answer_client: object,
) -> RecallService:
    settings = load_settings()
    return RecallService(
        settings,
        embedding_client=embedding_client,
        rag_answer_client=rag_answer_client,  # type: ignore[arg-type]
        session_factory=session_factory,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archived_session_rejects_recall_with_zero_side_effects(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    dataset = await seed_dataset(postgres_session_factory)
    session = await create_active_session(postgres_session_factory)
    await SessionService(session_factory=postgres_session_factory).archive_session(
        session.session_uuid
    )
    embedding = FakeEmbeddingClient(load_settings().embedding_dimensions)
    rag = RecordingRagAnswerClient()
    service = recall_service_for(
        postgres_session_factory, embedding_client=embedding, rag_answer_client=rag
    )

    try:
        with pytest.raises(SofiasMemoryError) as exc_info:
            await service.recall(
                RecallRequest(
                    query="memory",
                    datasets=[dataset.slug],
                    session_id=session.session_id,
                )
            )

        assert exc_info.value.code.value == "SESSION_ARCHIVED"
        assert exc_info.value.status_code == 409
        assert embedding.calls == []
        assert rag.calls == []

        async with postgres_session_factory() as raw_session:
            existing_query = await raw_session.scalar(
                select(Query).where(Query.session_id == session.session_uuid)
            )
        assert existing_query is None
    finally:
        await cleanup_sessions(postgres_session_factory, {session.session_uuid})
        await cleanup_datasets(postgres_session_factory, {dataset.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_snapshot_excludes_entry_appended_after_admission(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """SS 12/38: the admission transaction (including the Session Context
    snapshot) commits before embeddings/retrieval/generation ever run. An
    entry appended for real, mid-generation, must never appear in this
    Recall's context or persisted ``session_context_entry_ids`` -- even
    though it is fully committed and visible to any *new* Recall by the time
    generation finishes."""

    dataset = await seed_dataset(postgres_session_factory)
    session = await create_active_session(postgres_session_factory)
    entry_service = SessionEntryService(session_factory=postgres_session_factory)
    before = await entry_service.append_entry(
        session.session_uuid,
        SessionEntryCreateRequest(role="user", content="Entry present before admission."),
    )
    appended_during_generation = {"entry_id": None}

    async def append_mid_generation() -> None:
        after = await entry_service.append_entry(
            session.session_uuid,
            SessionEntryCreateRequest(role="user", content="Entry appended during generation."),
        )
        appended_during_generation["entry_id"] = after.entry_id

    embedding = FakeEmbeddingClient(load_settings().embedding_dimensions)
    rag = SideEffectRagAnswerClient(append_mid_generation)
    service = recall_service_for(
        postgres_session_factory, embedding_client=embedding, rag_answer_client=rag
    )
    entry_ids_to_cleanup: set[UUID] = set()
    query_ids: set[UUID] = set()

    try:
        result = await service.recall(
            RecallRequest(
                query="memory",
                datasets=[dataset.slug],
                mode="rag",
                session_id=session.session_id,
                include_session_context=True,
            )
        )
        query_ids.add(result.query_id)

        assert rag.calls
        _, context = rag.calls[0]
        assert before.content in context
        assert "Entry appended during generation." not in context
        assert appended_during_generation["entry_id"] is not None
        entry_ids_to_cleanup.add(appended_during_generation["entry_id"])

        async with postgres_session_factory() as raw_session:
            persisted = await raw_session.get(Query, result.query_id)
        assert persisted is not None
        assert persisted.session_context_entry_ids == [before.entry_id]
    finally:
        await cleanup_queries(postgres_session_factory, query_ids)
        await cleanup_sessions(postgres_session_factory, {session.session_uuid})
        await cleanup_datasets(postgres_session_factory, {dataset.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archive_after_admission_does_not_cancel_in_flight_recall(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """SS 12: the Session row lock is only held for the short admission
    transaction. Once released, a real archive can proceed concurrently --
    it must never retroactively fail or cancel a Recall whose admission
    already committed."""

    dataset = await seed_dataset(postgres_session_factory)
    session = await create_active_session(postgres_session_factory)
    entry_service = SessionEntryService(session_factory=postgres_session_factory)
    entry = await entry_service.append_entry(
        session.session_uuid,
        SessionEntryCreateRequest(role="user", content="Prior turn."),
    )

    async def archive_mid_generation() -> None:
        await SessionService(session_factory=postgres_session_factory).archive_session(
            session.session_uuid
        )

    embedding = FakeEmbeddingClient(load_settings().embedding_dimensions)
    rag = SideEffectRagAnswerClient(archive_mid_generation)
    service = recall_service_for(
        postgres_session_factory, embedding_client=embedding, rag_answer_client=rag
    )
    query_ids: set[UUID] = set()

    try:
        result = await service.recall(
            RecallRequest(
                query="memory",
                datasets=[dataset.slug],
                mode="rag",
                session_id=session.session_id,
                include_session_context=True,
            )
        )
        query_ids.add(result.query_id)

        assert result.answer == "Grounded answer."
        assert result.session_uuid == session.session_uuid

        async with postgres_session_factory() as raw_session:
            final_session = await raw_session.get(Session, session.session_uuid)
            persisted_query = await raw_session.get(Query, result.query_id)
        assert final_session is not None
        assert final_session.status == SessionStatus.ARCHIVED
        assert persisted_query is not None
        assert persisted_query.session_id == session.session_uuid
        assert persisted_query.session_context_entry_ids == [entry.entry_id]
    finally:
        await cleanup_queries(postgres_session_factory, query_ids)
        await cleanup_sessions(postgres_session_factory, {session.session_uuid})
        await cleanup_datasets(postgres_session_factory, {dataset.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_lazy_creation_converges_to_one_session(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """SS 7/9: two concurrent Recalls that are each the first-ever caller
    for a brand-new ``session_id`` must race-safely resolve to exactly one
    Session row (mirrors the SM-601 ``get_or_create_by_key`` guarantee),
    while each Recall still gets its own independent, correctly-associated
    Query."""

    dataset = await seed_dataset(postgres_session_factory)
    new_session_key = f"sm604-lazy-{uuid4().hex}"

    async def attempt(index: int) -> UUID:
        embedding = FakeEmbeddingClient(load_settings().embedding_dimensions)
        rag = RecordingRagAnswerClient()
        service = recall_service_for(
            postgres_session_factory, embedding_client=embedding, rag_answer_client=rag
        )
        result = await service.recall(
            RecallRequest(
                query=f"memory-{index}",
                datasets=[dataset.slug],
                mode="chunks",
                session_id=new_session_key,
            )
        )
        assert result.session_uuid is not None
        return result.query_id

    query_ids: set[UUID] = set()
    session_id: UUID | None = None

    try:
        query_ids_list = await asyncio.gather(*(attempt(i) for i in range(5)))
        query_ids.update(query_ids_list)
        assert len(set(query_ids_list)) == 5

        async with postgres_session_factory() as raw_session:
            resolved = await raw_session.scalar(
                select(Session).where(Session.key == new_session_key)
            )
            assert resolved is not None
            session_id = resolved.id

            persisted_queries = list(
                await raw_session.scalars(select(Query).where(Query.id.in_(query_ids_list)))
            )
        assert len(persisted_queries) == 5
        assert {query.session_id for query in persisted_queries} == {session_id}
    finally:
        await cleanup_queries(postgres_session_factory, query_ids)
        if session_id is not None:
            await cleanup_sessions(postgres_session_factory, {session_id})
        await cleanup_datasets(postgres_session_factory, {dataset.id})
