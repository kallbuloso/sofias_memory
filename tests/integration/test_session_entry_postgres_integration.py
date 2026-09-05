"""Real-PostgreSQL tests for the SM-603 SessionEntry API.

Proves what a fake in-memory Unit of Work cannot: the real DB-level trim
invariant (migration 0013), real partial-unique-index enforcement for safe
replay (including genuine concurrent-transaction races), and that the
append/archive admission barrier linearizes correctly against real
PostgreSQL row locking. Requires migrations already applied through 0013.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import load_settings
from sofias_memory.domain import SessionStatus
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Query, Session, SessionEntry
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.session_entries import SessionEntryCreateRequest
from sofias_memory.schemas.sessions import SessionCreateRequest, SessionResult
from sofias_memory.services.session_entries import SessionEntryService
from sofias_memory.services.sessions import SessionService

POSTGRES_SESSION_ENTRY_ENV = "SOFIAS_MEMORY_RUN_SESSION_ENTRY_POSTGRES_TESTS"


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    if os.environ.get(POSTGRES_SESSION_ENTRY_ENV) != "1":
        pytest.skip(f"set {POSTGRES_SESSION_ENTRY_ENV}=1 to run SessionEntry PostgreSQL tests")

    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        yield create_session_factory(engine)
    finally:
        await dispose_async_engine(engine)


async def cleanup_sessions(session_factory: AsyncSessionFactory, session_ids: set[object]) -> None:
    if not session_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Session).where(Session.id.in_(session_ids)))
        await session.commit()


async def cleanup_queries(session_factory: AsyncSessionFactory, query_ids: set[object]) -> None:
    # Query -> Session is ON DELETE SET NULL (independent audit record), so
    # deleting the Session never removes an associated Query row.
    if not query_ids:
        return
    async with session_factory() as session:
        await session.execute(delete(Query).where(Query.id.in_(query_ids)))
        await session.commit()


async def create_active_session(session_factory: AsyncSessionFactory) -> SessionResult:
    return await SessionService(session_factory=session_factory).create_session(
        SessionCreateRequest(session_id=f"sm603-{uuid4().hex}")
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_db_rejects_untrimmed_external_id_bypassing_the_api(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Migration 0013's authoritative invariant: even a direct INSERT that
    bypasses normalize_session_entry_external_id() must fail."""

    session = await create_active_session(postgres_session_factory)

    try:
        async with postgres_session_factory() as raw_session:
            raw_session.add(
                SessionEntry(
                    id=uuid4(),
                    session_id=session.session_uuid,
                    external_id=" foo ",
                    role="user",
                    content="hi",
                    metadata_={},
                )
            )
            with pytest.raises(IntegrityError):
                await raw_session.flush()
    finally:
        await cleanup_sessions(postgres_session_factory, {session.session_uuid})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_api_normalizes_external_id_before_insert(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session = await create_active_session(postgres_session_factory)

    try:
        result = await SessionEntryService(session_factory=postgres_session_factory).append_entry(
            session.session_uuid,
            SessionEntryCreateRequest(external_id="  foo  ", role="user", content="hi"),
        )
        assert result.external_id == "foo"

        async with postgres_session_factory() as raw_session:
            persisted = await raw_session.scalar(
                select(SessionEntry).where(SessionEntry.id == result.entry_id)
            )
        assert persisted is not None
        assert persisted.external_id == "foo"
    finally:
        await cleanup_sessions(postgres_session_factory, {session.session_uuid})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_safe_replay_same_external_id_same_payload_creates_one_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session = await create_active_session(postgres_session_factory)
    request = SessionEntryCreateRequest(external_id="ext-1", role="user", content="hi")

    try:
        first = await SessionEntryService(session_factory=postgres_session_factory).append_entry(
            session.session_uuid, request
        )
        second = await SessionEntryService(session_factory=postgres_session_factory).append_entry(
            session.session_uuid, request
        )

        assert first.entry_id == second.entry_id

        async with postgres_session_factory() as raw_session:
            count = await raw_session.scalar(
                select(func.count())
                .select_from(SessionEntry)
                .where(SessionEntry.session_id == session.session_uuid)
            )
        assert count == 1
    finally:
        await cleanup_sessions(postgres_session_factory, {session.session_uuid})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_safe_replay_same_external_id_different_payload_is_conflict(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session = await create_active_session(postgres_session_factory)

    try:
        await SessionEntryService(session_factory=postgres_session_factory).append_entry(
            session.session_uuid,
            SessionEntryCreateRequest(external_id="ext-1", role="user", content="hi"),
        )

        with pytest.raises(SofiasMemoryError) as excinfo:
            await SessionEntryService(session_factory=postgres_session_factory).append_entry(
                session.session_uuid,
                SessionEntryCreateRequest(external_id="ext-1", role="user", content="different"),
            )
        assert excinfo.value.status_code == 409
        assert excinfo.value.code.value == "IDEMPOTENCY_CONFLICT"

        async with postgres_session_factory() as raw_session:
            count = await raw_session.scalar(
                select(func.count())
                .select_from(SessionEntry)
                .where(SessionEntry.session_id == session.session_uuid)
            )
        assert count == 1
    finally:
        await cleanup_sessions(postgres_session_factory, {session.session_uuid})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_external_id_across_different_sessions_is_allowed(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session_a = await create_active_session(postgres_session_factory)
    session_b = await create_active_session(postgres_session_factory)
    external_id = f"shared-{uuid4().hex}"

    try:
        result_a = await SessionEntryService(session_factory=postgres_session_factory).append_entry(
            session_a.session_uuid,
            SessionEntryCreateRequest(external_id=external_id, role="user", content="a"),
        )
        result_b = await SessionEntryService(session_factory=postgres_session_factory).append_entry(
            session_b.session_uuid,
            SessionEntryCreateRequest(external_id=external_id, role="user", content="b"),
        )

        assert result_a.entry_id != result_b.entry_id
        assert result_a.external_id == result_b.external_id == external_id
    finally:
        await cleanup_sessions(
            postgres_session_factory, {session_a.session_uuid, session_b.session_uuid}
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_same_external_id_same_payload_converges_to_one_row(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session = await create_active_session(postgres_session_factory)
    request = SessionEntryCreateRequest(external_id="ext-race", role="user", content="hi")

    async def attempt() -> object:
        return await SessionEntryService(session_factory=postgres_session_factory).append_entry(
            session.session_uuid, request
        )

    try:
        results = await asyncio.gather(*(attempt() for _ in range(5)))

        entry_ids = {result.entry_id for result in results}
        assert entry_ids == {results[0].entry_id}

        async with postgres_session_factory() as raw_session:
            count = await raw_session.scalar(
                select(func.count())
                .select_from(SessionEntry)
                .where(SessionEntry.session_id == session.session_uuid)
            )
        assert count == 1
    finally:
        await cleanup_sessions(postgres_session_factory, {session.session_uuid})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_same_external_id_different_payload_exactly_one_winner(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session = await create_active_session(postgres_session_factory)

    async def attempt(index: int) -> tuple[str, int]:
        try:
            await SessionEntryService(session_factory=postgres_session_factory).append_entry(
                session.session_uuid,
                SessionEntryCreateRequest(
                    external_id="ext-conflict-race", role="user", content=f"content-{index}"
                ),
            )
            return ("ok", 201)
        except SofiasMemoryError as error:
            return ("conflict", error.status_code)

    try:
        outcomes = await asyncio.gather(*(attempt(i) for i in range(5)))

        successes = [outcome for outcome in outcomes if outcome[0] == "ok"]
        conflicts = [outcome for outcome in outcomes if outcome[0] == "conflict"]
        assert len(successes) == 1
        assert len(conflicts) == 4
        assert all(status == 409 for _, status in conflicts)

        async with postgres_session_factory() as raw_session:
            count = await raw_session.scalar(
                select(func.count())
                .select_from(SessionEntry)
                .where(SessionEntry.session_id == session.session_uuid)
            )
        assert count == 1
    finally:
        await cleanup_sessions(postgres_session_factory, {session.session_uuid})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_append_vs_archive_linearizes_and_never_corrupts(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Real row-lock race: append (no external_id, always a fresh create) vs
    archive on the same Session. Whichever wins the Session row lock first
    completes before the other proceeds -- both outcomes are valid, but they
    must be mutually consistent and never raise an unexpected exception."""

    session = await create_active_session(postgres_session_factory)

    async def do_append() -> tuple[str, object]:
        try:
            result = await SessionEntryService(
                session_factory=postgres_session_factory
            ).append_entry(
                session.session_uuid,
                SessionEntryCreateRequest(role="user", content="race"),
            )
            return ("ok", result)
        except SofiasMemoryError as error:
            return ("archived", error.code.value)

    try:
        (append_outcome, append_value), _archive_result = await asyncio.gather(
            do_append(),
            SessionService(session_factory=postgres_session_factory).archive_session(
                session.session_uuid
            ),
        )

        async with postgres_session_factory() as raw_session:
            entry_count = await raw_session.scalar(
                select(func.count())
                .select_from(SessionEntry)
                .where(SessionEntry.session_id == session.session_uuid)
            )
            final_session = await raw_session.get(Session, session.session_uuid)

        assert final_session is not None
        assert final_session.status == SessionStatus.ARCHIVED

        if append_outcome == "ok":
            assert entry_count == 1
        else:
            assert append_value == "SESSION_ARCHIVED"
            assert entry_count == 0
    finally:
        await cleanup_sessions(postgres_session_factory, {session.session_uuid})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entries_observable_from_new_service_instance_after_separate_commits(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session = await create_active_session(postgres_session_factory)

    try:
        # Each append_entry call opens/commits its own transaction, so
        # PostgreSQL's `now()` (constant within one transaction) actually
        # differs between entries -- see the SM-601 ordering test for why
        # that matters for asserting (created_at, id) order.
        for i in range(3):
            await SessionEntryService(session_factory=postgres_session_factory).append_entry(
                session.session_uuid,
                SessionEntryCreateRequest(role="user", content=f"turn-{i}"),
            )

        listed = await SessionEntryService(session_factory=postgres_session_factory).list_entries(
            session.session_uuid, limit=50, offset=0, ascending=True
        )

        assert listed.total == 3
        assert [item.content for item in listed.items] == ["turn-0", "turn-1", "turn-2"]
    finally:
        await cleanup_sessions(postgres_session_factory, {session.session_uuid})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_queries_observable_from_new_service_instance(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    session = await create_active_session(postgres_session_factory)
    query = Query(
        id=uuid4(),
        query_text="what happened?",
        dataset_ids=[],
        mode="chunks",
        answer=None,
        references={},
        timings={},
        model=None,
        session_id=session.session_uuid,
        session_context_entry_ids=[],
    )

    try:
        async with PostgresUnitOfWork(postgres_session_factory) as uow:
            await uow.queries.add(query)
            await uow.commit()

        listed = await SessionEntryService(session_factory=postgres_session_factory).list_queries(
            session.session_uuid, limit=50, offset=0
        )

        assert listed.total == 1
        assert listed.items[0].query_id == query.id
        assert listed.items[0].query_text == "what happened?"
        assert listed.items[0].answer is None
    finally:
        await cleanup_queries(postgres_session_factory, {query.id})
        await cleanup_sessions(postgres_session_factory, {session.session_uuid})
