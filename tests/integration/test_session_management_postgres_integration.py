"""Real-PostgreSQL tests for the SM-602 Session Management service.

Proves what a fake in-memory Unit of Work cannot: real unique-constraint
enforcement for explicit create (including genuine concurrent-transaction
races), and that lifecycle mutations (PATCH/archive/restore) are durably
observable from a brand-new connection/UnitOfWork. Requires migrations
already applied through 0012 against the configured PostgreSQL database.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.config import load_settings
from sofias_memory.domain import SessionStatus
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.models import Session
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.schemas.sessions import SessionCreateRequest, SessionUpdateRequest
from sofias_memory.services.sessions import SessionService

POSTGRES_SESSION_MANAGEMENT_ENV = "SOFIAS_MEMORY_RUN_SESSION_MANAGEMENT_POSTGRES_TESTS"


@pytest_asyncio.fixture()
async def postgres_session_factory() -> AsyncIterator[AsyncSessionFactory]:
    if os.environ.get(POSTGRES_SESSION_MANAGEMENT_ENV) != "1":
        pytest.skip(
            f"set {POSTGRES_SESSION_MANAGEMENT_ENV}=1 to run Session management PostgreSQL tests"
        )

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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_session_explicit_and_read_back_from_new_connection(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    key = f"sm602-create-{uuid4().hex}"
    service = SessionService(session_factory=postgres_session_factory)
    created = await service.create_session(
        SessionCreateRequest(session_id=key, name="Planning", metadata={"origin": "test"})
    )

    try:
        assert created.session_id == key
        assert created.name == "Planning"
        assert created.status == SessionStatus.ACTIVE

        # Fresh service instance -> fresh UnitOfWork/connection.
        fetched = await SessionService(session_factory=postgres_session_factory).get_session(
            created.session_uuid
        )
        assert fetched.session_id == key
        assert fetched.metadata == {"origin": "test"}
    finally:
        await cleanup_sessions(postgres_session_factory, {created.session_uuid})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_session_duplicate_session_id_is_real_unique_violation(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    key = f"sm602-dup-{uuid4().hex}"
    service = SessionService(session_factory=postgres_session_factory)
    first = await service.create_session(SessionCreateRequest(session_id=key))

    try:
        with pytest.raises(SofiasMemoryError) as excinfo:
            await service.create_session(SessionCreateRequest(session_id=key))
        assert excinfo.value.status_code == 409
    finally:
        await cleanup_sessions(postgres_session_factory, {first.session_uuid})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_explicit_create_same_session_id_exactly_one_wins(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    key = f"sm602-race-{uuid4().hex}"

    async def attempt() -> object:
        service = SessionService(session_factory=postgres_session_factory)
        try:
            result = await service.create_session(SessionCreateRequest(session_id=key))
            return ("ok", result.session_uuid)
        except SofiasMemoryError as error:
            return ("error", error.status_code)

    outcomes = await asyncio.gather(*(attempt() for _ in range(5)))

    successes = [outcome for outcome in outcomes if outcome[0] == "ok"]
    conflicts = [outcome for outcome in outcomes if outcome[0] == "error"]

    try:
        assert len(successes) == 1
        assert len(conflicts) == 4
        assert all(status == 409 for _, status in conflicts)
    finally:
        winning_ids = {outcome[1] for outcome in successes}
        await cleanup_sessions(postgres_session_factory, winning_ids)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_sessions_filters_by_status_and_exact_case_sensitive_session_id(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    token = uuid4().hex
    service = SessionService(session_factory=postgres_session_factory)
    lower = await service.create_session(SessionCreateRequest(session_id=f"case-{token}"))
    upper = await service.create_session(SessionCreateRequest(session_id=f"CASE-{token}"))
    archived_source = await service.create_session(
        SessionCreateRequest(session_id=f"archived-{token}")
    )
    await service.archive_session(archived_source.session_uuid)

    try:
        exact_lower = await service.list_sessions(
            limit=50, offset=0, status=None, session_id=f"case-{token}"
        )
        exact_upper = await service.list_sessions(
            limit=50, offset=0, status=None, session_id=f"CASE-{token}"
        )
        archived_only = await service.list_sessions(
            limit=50, offset=0, status=SessionStatus.ARCHIVED, session_id=None
        )

        assert [item.session_uuid for item in exact_lower.items] == [lower.session_uuid]
        assert [item.session_uuid for item in exact_upper.items] == [upper.session_uuid]
        assert archived_source.session_uuid in {item.session_uuid for item in archived_only.items}
        assert lower.session_uuid not in {item.session_uuid for item in archived_only.items}
    finally:
        await cleanup_sessions(
            postgres_session_factory,
            {lower.session_uuid, upper.session_uuid, archived_source.session_uuid},
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_patch_persists_and_is_observable_from_new_unit_of_work(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    service = SessionService(session_factory=postgres_session_factory)
    created = await service.create_session(
        SessionCreateRequest(session_id=f"sm602-patch-{uuid4().hex}", name="Before")
    )

    try:
        await SessionService(session_factory=postgres_session_factory).update_session(
            created.session_uuid,
            SessionUpdateRequest(name="After", metadata={"k": "v"}),
        )

        reloaded = await SessionService(session_factory=postgres_session_factory).get_session(
            created.session_uuid
        )
        assert reloaded.name == "After"
        assert reloaded.metadata == {"k": "v"}
        assert reloaded.updated_at > created.updated_at
    finally:
        await cleanup_sessions(postgres_session_factory, {created.session_uuid})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archive_then_restore_lifecycle_persists_across_new_unit_of_work(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    service = SessionService(session_factory=postgres_session_factory)
    created = await service.create_session(
        SessionCreateRequest(session_id=f"sm602-lifecycle-{uuid4().hex}")
    )

    try:
        archived = await SessionService(session_factory=postgres_session_factory).archive_session(
            created.session_uuid
        )
        assert archived.status == SessionStatus.ARCHIVED
        assert archived.archived_at is not None

        reloaded_archived = await SessionService(
            session_factory=postgres_session_factory
        ).get_session(created.session_uuid)
        assert reloaded_archived.status == SessionStatus.ARCHIVED
        assert reloaded_archived.archived_at is not None

        restored = await SessionService(session_factory=postgres_session_factory).restore_session(
            created.session_uuid
        )
        assert restored.status == SessionStatus.ACTIVE
        assert restored.archived_at is None

        reloaded_active = await SessionService(
            session_factory=postgres_session_factory
        ).get_session(created.session_uuid)
        assert reloaded_active.status == SessionStatus.ACTIVE
        assert reloaded_active.archived_at is None
    finally:
        await cleanup_sessions(postgres_session_factory, {created.session_uuid})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_archive_and_restore_serialize_to_coherent_final_state(
    postgres_session_factory: AsyncSessionFactory,
) -> None:
    """Two independent Units of Work race `archive` vs `restore` on the same
    Session. `SELECT ... FOR UPDATE` (SessionRepository.get_by_id_for_update)
    serializes them at the PostgreSQL level, so either order is a valid
    outcome -- this proves the race is safe, not which side wins."""

    created = await SessionService(session_factory=postgres_session_factory).create_session(
        SessionCreateRequest(session_id=f"sm602-archive-restore-race-{uuid4().hex}")
    )

    try:
        archive_service = SessionService(session_factory=postgres_session_factory)
        restore_service = SessionService(session_factory=postgres_session_factory)

        archived_outcome, restored_outcome = await asyncio.gather(
            archive_service.archive_session(created.session_uuid),
            restore_service.restore_session(created.session_uuid),
        )

        # No unexpected exception propagated out of either call (asyncio.gather
        # would have raised), and each call observed a well-formed result for
        # the same Session.
        assert archived_outcome.session_uuid == created.session_uuid
        assert restored_outcome.session_uuid == created.session_uuid

        final = await SessionService(session_factory=postgres_session_factory).get_session(
            created.session_uuid
        )
        if final.status == SessionStatus.ACTIVE:
            assert final.archived_at is None
        else:
            assert final.status == SessionStatus.ARCHIVED
            assert final.archived_at is not None

        # Exactly one row survives the race -- no duplication/corruption.
        async with postgres_session_factory() as raw_session:
            row_count = await raw_session.scalar(
                select(func.count()).select_from(Session).where(Session.id == created.session_uuid)
            )
        assert row_count == 1

        # Lifecycle remains usable after the race, regardless of which side
        # won it.
        after_archive = await SessionService(
            session_factory=postgres_session_factory
        ).archive_session(created.session_uuid)
        assert after_archive.status == SessionStatus.ARCHIVED
        after_restore = await SessionService(
            session_factory=postgres_session_factory
        ).restore_session(created.session_uuid)
        assert after_restore.status == SessionStatus.ACTIVE
    finally:
        await cleanup_sessions(postgres_session_factory, {created.session_uuid})
