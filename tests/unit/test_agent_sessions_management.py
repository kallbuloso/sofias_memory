from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.domain import AgentStatus, SessionStatus
from sofias_memory.infrastructure.postgres.models import Agent, Session
from sofias_memory.infrastructure.postgres.repositories.agent_sessions import AgentSessionRow
from sofias_memory.schemas.agent_sessions import AgentSessionListResult, AgentSessionResult
from sofias_memory.services.agent_sessions import (
    AgentSessionService,
    AgentSessionUnitOfWork,
    UnitOfWorkFactory,
)

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_agent_session_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AgentSessionResult(
            session_uuid=uuid4(),
            session_id="sess-1",
            name=None,
            status=SessionStatus.ACTIVE,
            association_created_at=CREATED_AT,
            role="assistant",  # type: ignore[call-arg]
        )


def test_agent_session_result_never_exposes_transcript_fields() -> None:
    result = AgentSessionResult(
        session_uuid=uuid4(),
        session_id="sess-1",
        name="Session One",
        status=SessionStatus.ACTIVE,
        association_created_at=CREATED_AT,
    )
    dumped = result.model_dump()
    assert set(dumped) == {"session_uuid", "session_id", "name", "status", "association_created_at"}
    for forbidden in (
        "entries",
        "session_entries",
        "content",
        "role",
        "queries",
        "pipeline_runs",
        "context",
        "transcript",
    ):
        assert forbidden not in dumped


def test_agent_session_list_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AgentSessionListResult(items=[], total=0)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Service (fake in-memory Unit of Work)
# ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self.agents: dict[UUID, Agent] = {}
        self.sessions: dict[UUID, Session] = {}
        self.agent_sessions: dict[tuple[UUID, UUID], datetime] = {}
        self.commits = 0


class FakeAgentRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, agent_id: UUID) -> Agent | None:
        return self._store.agents.get(agent_id)


class FakeSessionRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def get_by_id(self, session_id: UUID) -> Session | None:
        return self._store.sessions.get(session_id)


class FakeAgentSessionRepository:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.ensure_calls = 0
        self.delete_calls = 0

    async def ensure(self, *, agent_id: UUID, session_id: UUID) -> None:
        self.ensure_calls += 1
        key = (agent_id, session_id)
        if key not in self._store.agent_sessions:
            self._store.agent_sessions[key] = CREATED_AT

    async def delete(self, *, agent_id: UUID, session_id: UUID) -> None:
        self.delete_calls += 1
        self._store.agent_sessions.pop((agent_id, session_id), None)

    async def get_one_for_agent_and_session(
        self,
        *,
        agent_id: UUID,
        session_id: UUID,
    ) -> AgentSessionRow | None:
        created_at = self._store.agent_sessions.get((agent_id, session_id))
        if created_at is None:
            return None
        return self._row(session_id, created_at)

    async def list_for_agent(self, agent_id: UUID) -> list[AgentSessionRow]:
        rows = [
            self._row(s_id, created_at)
            for (a_id, s_id), created_at in self._store.agent_sessions.items()
            if a_id == agent_id
        ]
        rows.sort(key=lambda row: (row.association_created_at, row.session_uuid))
        return rows

    def _row(self, session_id: UUID, created_at: datetime) -> AgentSessionRow:
        session = self._store.sessions[session_id]
        return AgentSessionRow(
            session_uuid=session.id,
            session_id=session.key,
            name=session.name,
            status=session.status,
            association_created_at=created_at,
        )


class FakeUnitOfWork:
    def __init__(self, store: FakeStore) -> None:
        self._store = store
        self.agents = FakeAgentRepository(store)
        self.sessions = FakeSessionRepository(store)
        self.agent_sessions = FakeAgentSessionRepository(store)

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def commit(self) -> None:
        self._store.commits += 1


def service_for(store: FakeStore) -> AgentSessionService:
    def create_uow() -> AgentSessionUnitOfWork:
        return cast(AgentSessionUnitOfWork, FakeUnitOfWork(store))

    return AgentSessionService(unit_of_work_factory=cast(UnitOfWorkFactory, create_uow))


def make_agent(*, status: AgentStatus = AgentStatus.ACTIVE) -> Agent:
    return Agent(
        id=uuid4(),
        name=f"agent-{uuid4().hex[:8]}",
        display_name=None,
        description=None,
        instructions=None,
        metadata_={},
        status=status,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
        archived_at=None,
    )


def make_session(*, status: SessionStatus = SessionStatus.ACTIVE) -> Session:
    return Session(
        id=uuid4(),
        key=f"sess-{uuid4().hex[:8]}",
        name="A Session",
        status=status,
        metadata_={},
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
        archived_at=CREATED_AT if status == SessionStatus.ARCHIVED else None,
    )


def seed(store: FakeStore, *, agent: Agent, session: Session) -> None:
    store.agents[agent.id] = agent
    store.sessions[session.id] = session


# --- Agent/Session existence -------------------------------------------------


@pytest.mark.asyncio
async def test_associate_session_agent_missing_is_404() -> None:
    store = FakeStore()
    session = make_session()
    store.sessions[session.id] = session
    service = service_for(store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.associate_session(uuid4(), session.id)
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_associate_session_session_missing_is_404() -> None:
    store = FakeStore()
    agent = make_agent()
    store.agents[agent.id] = agent
    service = service_for(store)

    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.associate_session(agent.id, uuid4())
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_list_sessions_agent_missing_is_404() -> None:
    store = FakeStore()
    service = service_for(store)
    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.list_sessions(uuid4())
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_remove_session_agent_missing_is_404() -> None:
    store = FakeStore()
    service = service_for(store)
    with pytest.raises(SofiasMemoryError) as excinfo:
        await service.remove_session(uuid4(), uuid4())
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_remove_session_missing_session_uuid_is_204_not_404() -> None:
    """No Session pre-check on DELETE: a Session UUID that references
    nothing at all simply resolves to "association absent", not an error."""

    store = FakeStore()
    agent = make_agent()
    store.agents[agent.id] = agent
    service = service_for(store)

    await service.remove_session(agent.id, uuid4())  # must not raise


# --- PUT idempotency / created_at semantics ----------------------------------


@pytest.mark.asyncio
async def test_associate_session_creates_association() -> None:
    store = FakeStore()
    agent = make_agent()
    session = make_session()
    seed(store, agent=agent, session=session)
    service = service_for(store)

    result = await service.associate_session(agent.id, session.id)

    assert result.session_uuid == session.id
    assert result.session_id == session.key
    assert len(store.agent_sessions) == 1


@pytest.mark.asyncio
async def test_associate_session_replay_preserves_created_at() -> None:
    store = FakeStore()
    agent = make_agent()
    session = make_session()
    seed(store, agent=agent, session=session)
    service = service_for(store)

    first = await service.associate_session(agent.id, session.id)
    second = await service.associate_session(agent.id, session.id)

    assert second.association_created_at == first.association_created_at
    assert len(store.agent_sessions) == 1


class LaterFakeAgentSessionRepository(FakeAgentSessionRepository):
    """Stamps a strictly later ``created_at`` than :data:`CREATED_AT`,
    simulating time passing between a DELETE and a subsequent PUT."""

    async def ensure(self, *, agent_id: UUID, session_id: UUID) -> None:
        self.ensure_calls += 1
        key = (agent_id, session_id)
        if key not in self._store.agent_sessions:
            self._store.agent_sessions[key] = datetime(2026, 1, 2, tzinfo=UTC)


class LaterUnitOfWork(FakeUnitOfWork):
    def __init__(self, store: FakeStore) -> None:
        super().__init__(store)
        self.agent_sessions = LaterFakeAgentSessionRepository(store)


@pytest.mark.asyncio
async def test_associate_session_after_delete_gets_new_created_at() -> None:
    """The concrete proof that created_at is not first-ever participation:
    delete then re-associate produces a strictly later created_at, and the
    original created_at is not retained anywhere after the delete."""

    store = FakeStore()
    agent = make_agent()
    session = make_session()
    seed(store, agent=agent, session=session)
    service = service_for(store)

    first = await service.associate_session(agent.id, session.id)
    await service.remove_session(agent.id, session.id)
    assert (agent.id, session.id) not in store.agent_sessions

    def later_uow_factory() -> AgentSessionUnitOfWork:
        return cast(AgentSessionUnitOfWork, LaterUnitOfWork(store))

    later_service = AgentSessionService(
        unit_of_work_factory=cast(UnitOfWorkFactory, later_uow_factory)
    )
    second = await later_service.associate_session(agent.id, session.id)

    assert second.association_created_at != first.association_created_at
    assert second.association_created_at > first.association_created_at


# --- DELETE -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_session_existing_association() -> None:
    store = FakeStore()
    agent = make_agent()
    session = make_session()
    seed(store, agent=agent, session=session)
    service = service_for(store)
    await service.associate_session(agent.id, session.id)
    assert len(store.agent_sessions) == 1

    await service.remove_session(agent.id, session.id)

    assert len(store.agent_sessions) == 0


@pytest.mark.asyncio
async def test_remove_session_missing_association_is_idempotent_noop() -> None:
    store = FakeStore()
    agent = make_agent()
    session = make_session()
    seed(store, agent=agent, session=session)
    service = service_for(store)

    await service.remove_session(agent.id, session.id)  # no association ever created
    await service.remove_session(agent.id, session.id)  # replay

    assert len(store.agent_sessions) == 0


# --- archived Agent / archived Session -----------------------------------------


@pytest.mark.asyncio
async def test_associate_session_archived_agent_accepted() -> None:
    store = FakeStore()
    agent = make_agent(status=AgentStatus.ARCHIVED)
    session = make_session()
    seed(store, agent=agent, session=session)
    service = service_for(store)

    result = await service.associate_session(agent.id, session.id)

    assert result.session_uuid == session.id


@pytest.mark.asyncio
async def test_associate_session_archived_session_accepted_and_visible_in_list() -> None:
    store = FakeStore()
    agent = make_agent()
    session = make_session(status=SessionStatus.ARCHIVED)
    seed(store, agent=agent, session=session)
    service = service_for(store)

    await service.associate_session(agent.id, session.id)
    listed = await service.list_sessions(agent.id)

    assert len(listed.items) == 1
    assert listed.items[0].status == SessionStatus.ARCHIVED


@pytest.mark.asyncio
async def test_list_sessions_empty_for_agent_with_no_associations() -> None:
    store = FakeStore()
    agent = make_agent()
    store.agents[agent.id] = agent
    service = service_for(store)

    result = await service.list_sessions(agent.id)

    assert result.items == []
