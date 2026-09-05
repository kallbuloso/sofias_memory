"""Real-PostgreSQL tests for the SM-601 Sessions persistence foundation.

Proves ADR-0012 / Feature Contract v0.3.0 Sessions: ``Session.key`` uniqueness
and case sensitivity, race-safe ``get_or_create_by_key`` lazy creation, the
delete policy matrix (``SessionEntry`` CASCADE, ``Query``/``PipelineRun`` SET
NULL), and ``SessionEntry``/``Query`` listing ordering. Requires migrations
already applied through 0012 against the configured PostgreSQL database.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from sofias_memory.config import load_settings
from sofias_memory.domain import PipelineRunStatus, PipelineType, SessionStatus
from sofias_memory.infrastructure.postgres import (
    PostgresUnitOfWork,
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import (
    PipelineRun,
    Query,
    Session,
    SessionEntry,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory

POSTGRES_SESSIONS_ENV = "SOFIAS_MEMORY_RUN_POSTGRES_SESSIONS_TESTS"


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    if os.environ.get(POSTGRES_SESSIONS_ENV) != "1":
        pytest.skip(f"set {POSTGRES_SESSIONS_ENV}=1 to run PostgreSQL Sessions tests")

    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


def build_session(*, key: str, name: str | None = None) -> Session:
    return Session(
        id=uuid4(),
        key=key,
        name=name,
        status=SessionStatus.ACTIVE,
        metadata_={},
    )


def build_entry(
    *,
    session_id: UUID,
    role: str = "user",
    content: str = "hello",
    external_id: str | None = None,
) -> SessionEntry:
    return SessionEntry(
        id=uuid4(),
        session_id=session_id,
        external_id=external_id,
        role=role,
        content=content,
        metadata_={},
    )


def build_query(*, session_id: UUID | None) -> Query:
    return Query(
        id=uuid4(),
        query_text="what happened?",
        dataset_ids=[],
        mode="chunks",
        answer=None,
        references={},
        timings={},
        model=None,
        session_id=session_id,
        session_context_entry_ids=[],
    )


def build_run(*, session_id: UUID | None) -> PipelineRun:
    return PipelineRun(
        id=uuid4(),
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=None,
        source_id=None,
        status=PipelineRunStatus.QUEUED,
        idempotency_key=None,
        payload_hash="a" * 64,
        input={},
        progress=0.0,
        current_step=None,
        attempt=0,
        worker_id=None,
        heartbeat_at=None,
        config_fingerprint="b" * 64,
        error_code=None,
        error_message=None,
        metrics={},
        started_at=None,
        finished_at=None,
        next_attempt_at=None,
        retry_of_run_id=None,
        session_id=session_id,
    )


async def cleanup_sessions(
    session_factory: AsyncSessionFactory,
    *,
    session_ids: set[UUID],
    query_ids: set[UUID] | None = None,
    run_ids: set[UUID] | None = None,
) -> None:
    async with session_factory() as session:
        if run_ids:
            await session.execute(delete(PipelineRun).where(PipelineRun.id.in_(run_ids)))
        if query_ids:
            await session.execute(delete(Query).where(Query.id.in_(query_ids)))
        if session_ids:
            await session.execute(delete(Session).where(Session.id.in_(session_ids)))
        await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_add_and_get_by_id_and_get_by_key(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    key = f"sm601-{uuid4().hex}"
    session = build_session(key=key, name="Planning")

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await uow.sessions.add(session)
            await uow.commit()

        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            by_id = await uow.sessions.get_by_id(session.id)
            by_key = await uow.sessions.get_by_key(key)
            await uow.commit()

        assert by_id is not None
        assert by_id.key == key
        assert by_id.name == "Planning"
        assert by_id.status == SessionStatus.ACTIVE
        assert by_key is not None
        assert by_key.id == session.id
    finally:
        await cleanup_sessions(postgres_session_factory, session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_key_is_case_sensitive(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    token = uuid4().hex
    lower = build_session(key=f"case-{token}")
    upper = build_session(key=f"CASE-{token}")

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await uow.sessions.add(lower)
            await uow.sessions.add(upper)
            await uow.commit()

        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            found_lower = await uow.sessions.get_by_key(f"case-{token}")
            found_upper = await uow.sessions.get_by_key(f"CASE-{token}")
            await uow.commit()

        assert found_lower is not None
        assert found_upper is not None
        assert found_lower.id == lower.id
        assert found_upper.id == upper.id
        assert found_lower.id != found_upper.id
    finally:
        await cleanup_sessions(postgres_session_factory, session_ids={lower.id, upper.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_or_create_by_key_never_updates_existing_session(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    key = f"sm601-idempotent-{uuid4().hex}"
    first = build_session(key=key, name="Original name")
    second_candidate = build_session(key=key, name="Different name")

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            resolved_first = await uow.sessions.get_or_create_by_key(first)
            await uow.commit()

        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            resolved_second = await uow.sessions.get_or_create_by_key(second_candidate)
            await uow.commit()

        assert resolved_first.id == resolved_second.id
        assert resolved_second.name == "Original name"
    finally:
        await cleanup_sessions(
            postgres_session_factory,
            session_ids={first.id, second_candidate.id},
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_or_create_by_key_concurrent_requests_converge_to_one_session(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    key = f"sm601-race-{uuid4().hex}"
    candidates = [build_session(key=key, name=f"candidate-{i}") for i in range(5)]

    async def resolve(candidate: Session) -> UUID:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            resolved = await uow.sessions.get_or_create_by_key(candidate)
            await uow.commit()
            return resolved.id

    try:
        resolved_ids = await asyncio.gather(*(resolve(candidate) for candidate in candidates))

        assert len(set(resolved_ids)) == 1

        async with postgres_session_factory() as session:
            result = await session.scalars(select(Session).where(Session.key == key))
            rows = list(result)
        assert len(rows) == 1
        assert rows[0].id == resolved_ids[0]
    finally:
        await cleanup_sessions(
            postgres_session_factory,
            session_ids={candidate.id for candidate in candidates},
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_list_paginated_filters_by_status_and_key(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    token = uuid4().hex
    active = build_session(key=f"list-active-{token}")
    archived = build_session(key=f"list-archived-{token}")
    archived.status = SessionStatus.ARCHIVED

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await uow.sessions.add(active)
            await uow.sessions.add(archived)
            await uow.commit()

        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            by_status, status_total = await uow.sessions.list_paginated(
                limit=50,
                offset=0,
                status=SessionStatus.ARCHIVED,
            )
            by_key, key_total = await uow.sessions.list_paginated(
                limit=50,
                offset=0,
                key=f"list-active-{token}",
            )
            await uow.commit()

        assert archived.id in {row.id for row in by_status}
        assert active.id not in {row.id for row in by_status}
        assert status_total >= 1
        assert [row.id for row in by_key] == [active.id]
        assert key_total == 1
    finally:
        await cleanup_sessions(postgres_session_factory, session_ids={active.id, archived.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_by_id_for_update_returns_locked_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session = build_session(key=f"sm601-lock-{uuid4().hex}")

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await uow.sessions.add(session)
            await uow.commit()

        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            locked = await uow.sessions.get_by_id_for_update(session.id)
            await uow.commit()

        assert locked is not None
        assert locked.id == session.id
    finally:
        await cleanup_sessions(postgres_session_factory, session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_entry_add_and_list_by_session_orders_deterministically(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session = build_session(key=f"sm601-entries-{uuid4().hex}")
    entries = [build_entry(session_id=session.id, content=f"turn-{i}") for i in range(3)]

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await uow.sessions.add(session)
            await uow.commit()

        # PostgreSQL's `now()` is constant within one transaction, so entries
        # committed together would share `created_at` and fall back to
        # `id` (a random UUID) as the tiebreaker. Committing each append in
        # its own transaction gives each a distinct `created_at`, matching
        # how SessionEntry appends actually happen in production (one
        # request per append) and letting this test assert on the
        # `(created_at, id)` ordering the contract requires without relying
        # on `id` randomness.
        for entry in entries:
            async with PostgresUnitOfWork(postgres_session_factory) as uow:
                await uow.session_entries.add(entry)
                await uow.commit()

        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            ascending = await uow.session_entries.list_by_session(session.id, ascending=True)
            descending = await uow.session_entries.list_by_session(session.id, ascending=False)
            await uow.commit()

        assert [entry.id for entry in ascending] == [entry.id for entry in entries]
        assert [entry.id for entry in descending] == list(reversed([entry.id for entry in entries]))
    finally:
        await cleanup_sessions(postgres_session_factory, session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_entry_external_id_is_unique_within_a_session(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session = build_session(key=f"sm601-external-id-dup-{uuid4().hex}")
    external_id = f"sofias-assistant:turn:{uuid4().hex}:user"
    first_entry = build_entry(session_id=session.id, external_id=external_id)
    duplicate_entry = build_entry(session_id=session.id, external_id=external_id)

    async with PostgresUnitOfWork(postgres_session_factory) as uow:
        await uow.sessions.add(session)
        await uow.session_entries.add(first_entry)
        await uow.commit()

    try:
        with pytest.raises(IntegrityError):
            async with PostgresUnitOfWork(postgres_session_factory) as uow:
                await uow.session_entries.add(duplicate_entry)
                await uow.commit()
    finally:
        await cleanup_sessions(postgres_session_factory, session_ids={session.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_entry_external_id_may_repeat_across_different_sessions(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    token = uuid4().hex
    external_id = f"sofias-assistant:turn:{token}:user"
    session_a = build_session(key=f"sm601-external-id-a-{token}")
    session_b = build_session(key=f"sm601-external-id-b-{token}")
    entry_a = build_entry(session_id=session_a.id, external_id=external_id)
    entry_b = build_entry(session_id=session_b.id, external_id=external_id)

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await uow.sessions.add(session_a)
            await uow.sessions.add(session_b)
            await uow.session_entries.add(entry_a)
            await uow.session_entries.add(entry_b)
            await uow.commit()

        async with postgres_session_factory() as raw_session:
            persisted_a = await raw_session.scalar(
                select(SessionEntry).where(SessionEntry.id == entry_a.id)
            )
            persisted_b = await raw_session.scalar(
                select(SessionEntry).where(SessionEntry.id == entry_b.id)
            )

        assert persisted_a is not None
        assert persisted_b is not None
        assert persisted_a.external_id == external_id
        assert persisted_b.external_id == external_id
    finally:
        await cleanup_sessions(
            postgres_session_factory,
            session_ids={session_a.id, session_b.id},
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_query_list_by_session_returns_newest_first(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session = build_session(key=f"sm601-queries-{uuid4().hex}")
    queries = [build_query(session_id=session.id) for _ in range(2)]

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await uow.sessions.add(session)
            await uow.commit()

        # See the SessionEntry ordering test: PostgreSQL's `now()` is
        # constant within one transaction, so separate commits are needed
        # for `created_at` to actually differ between queries.
        for query in queries:
            async with PostgresUnitOfWork(postgres_session_factory) as uow:
                await uow.queries.add(query)
                await uow.commit()

        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            listed = await uow.queries.list_by_session(session.id)
            await uow.commit()

        assert [query.id for query in listed] == [query.id for query in reversed(queries)]
    finally:
        await cleanup_sessions(
            postgres_session_factory,
            session_ids={session.id},
            query_ids={query.id for query in queries},
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_delete_cascades_to_session_entries(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session = build_session(key=f"sm601-cascade-{uuid4().hex}")
    entry = build_entry(session_id=session.id)

    async with PostgresUnitOfWork(postgres_session_factory) as uow:
        await uow.sessions.add(session)
        await uow.session_entries.add(entry)
        await uow.commit()

    async with postgres_session_factory() as raw_session:
        await raw_session.execute(delete(Session).where(Session.id == session.id))
        await raw_session.commit()

    async with postgres_session_factory() as raw_session:
        remaining = await raw_session.scalar(
            select(SessionEntry).where(SessionEntry.id == entry.id)
        )

    assert remaining is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_delete_sets_query_session_id_null(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session = build_session(key=f"sm601-query-setnull-{uuid4().hex}")
    query = build_query(session_id=session.id)

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await uow.sessions.add(session)
            await uow.queries.add(query)
            await uow.commit()

        async with postgres_session_factory() as raw_session:
            await raw_session.execute(delete(Session).where(Session.id == session.id))
            await raw_session.commit()

        async with postgres_session_factory() as raw_session:
            surviving_query = await raw_session.scalar(select(Query).where(Query.id == query.id))

        assert surviving_query is not None
        assert surviving_query.session_id is None
    finally:
        await cleanup_sessions(postgres_session_factory, session_ids=set(), query_ids={query.id})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_delete_sets_pipeline_run_session_id_null(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session = build_session(key=f"sm601-run-setnull-{uuid4().hex}")
    run = build_run(session_id=session.id)

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await uow.sessions.add(session)
            await uow.pipeline_runs.add(run)
            await uow.commit()

        async with postgres_session_factory() as raw_session:
            await raw_session.execute(delete(Session).where(Session.id == session.id))
            await raw_session.commit()

        async with postgres_session_factory() as raw_session:
            surviving_run = await raw_session.scalar(
                select(PipelineRun).where(PipelineRun.id == run.id)
            )

        assert surviving_run is not None
        assert surviving_run.session_id is None
    finally:
        await cleanup_sessions(postgres_session_factory, session_ids=set(), run_ids={run.id})
